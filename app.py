"""Streamlit UI for the Cuga + IBM Agent Identity showcase.

Run:  scripts/start.sh   (or)   .venv/bin/streamlit run app.py --server.port 8601
"""

from __future__ import annotations

import os

os.environ.setdefault("LOGURU_LEVEL", "WARNING")

import hashlib
import json
from datetime import datetime
from pathlib import Path

import streamlit as st

from agent_identity.async_runner import AsyncRunner
from agent_identity.bootstrap import deploy_all
from agent_identity.config import get_settings
from agent_identity.data import USERS
from agent_identity.errors import IdentityError, LLMEngineError
from agent_identity.llm_engines import ENGINES, FAKE_ENGINE, config_from_form
from agent_identity.logging_setup import get_logger, setup_logging
from agent_identity.models import AgentStatus
from agent_identity.runtime import APPROVER, DEVELOPER, Showcase
from agent_identity.ui_helpers import (
    FLOWS,
    audit_rows,
    explain_token,
    flow_by_key,
    flow_to_mermaid,
    flow_to_svg,
    inspect_token,
    registry_rows,
)
from agents import SUPERVISOR_NAME, SUPERVISOR_SPEC, WORKER_SPECS

st.set_page_config(page_title="Cuga + Agent Identity showcase", layout="wide")

SETTINGS = get_settings()
setup_logging(SETTINGS.log_level, SETTINGS.output_dir)
log = get_logger("ui")
ALL_SPECS = {**WORKER_SPECS, SUPERVISOR_NAME: SUPERVISOR_SPEC}


# --------------------------------------------------------------------------------------
# Shared resources (one per server process)
# --------------------------------------------------------------------------------------
@st.cache_resource(show_spinner="Starting the showcase (selecting the LLM engine, onboarding and deploying agents)...")
def get_app() -> dict:
    """Create the async runner, resolve the LLM engine and deploy all agents once."""
    runner = AsyncRunner()
    showcase = Showcase(settings=SETTINGS)  # resolves the engine: UI choice > env > detection > fallback
    runner.run(deploy_all(showcase), timeout=600)
    return {"runner": runner, "sc": showcase}


APP = get_app()
RUNNER: AsyncRunner = APP["runner"]
SC: Showcase = APP["sc"]


def names_by_id() -> dict[str, str]:
    return {r.agent_id: r.name for r in SC.backend.list_agents()}


# --------------------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------------------
PAGES = ["Overview", "How Agent Identity works", "Agent registry and lifecycle", "Chat with agents",
         "Token inspector", "Audit trail", "Harness Kit validation", "LLM engine", "Settings and status"]

with st.sidebar:
    st.title("Cuga + Agent Identity")
    page = st.radio("Navigate", PAGES, label_visibility="collapsed")
    st.divider()
    if SC.engine.engine == FAKE_ENGINE:
        st.warning("LLM: offline fake model. Chat shows policy and identity decisions only. See the LLM engine page.")
    else:
        st.success(f"LLM: {SC.engine.model} ({ENGINES[SC.engine.engine].label}, from {SC.engine.source})")
    st.caption(f"Identity backend: **{SC.settings.identity_backend}**")
    st.caption("License: Apache-2.0")


# --------------------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------------------
def page_overview() -> None:
    st.header("AI agents with a real identity")
    st.write(
        "This showcase combines the **Cuga agent framework**, the **Cuga Harness Kit** and the "
        "**IBM Agent Identity** model. Every agent is registered, approved by a second person, "
        "gets its own OAuth client and acts with short-lived, scoped tokens that are checked by "
        "each tool and written to an audit trail."
    )
    c1, c2, c3 = st.columns(3)
    c1.metric("Agents deployed", len(SC.runtimes))
    c2.metric("Audit events", len(SC.audit.events()))
    c3.metric("Denied decisions", len(SC.audit.events(outcome="denied")))
    st.subheader("Example agents")
    cols = st.columns(len(ALL_SPECS))
    for col, spec in zip(cols, ALL_SPECS.values()):
        with col:
            st.markdown(f"**{spec.title}**")
            st.caption(spec.use_case)
            st.write(spec.description)
            st.code(f"mode: {spec.access_mode}\nceiling: {', '.join(sorted(spec.allowed_scopes))}", language="text")
    st.subheader("Try it")
    st.markdown(
        "1. **How Agent Identity works** - step through the six identity flows.\n"
        "2. **Chat with agents** - ask as *Carol* or *Dave* and watch scopes, approvals and denials.\n"
        "3. **Agent registry and lifecycle** - suspend an agent (kill switch) and see tools stop working.\n"
        "4. **Harness Kit validation** - run the conformance suite."
    )
    st.info("Identity runs against a local mock of IBM Verify. Set `AI_IDENTITY_BACKEND=verify` "
            "to use a real tenant (reference implementation, see Docs/Architecture.md).")


def page_flows() -> None:
    st.header("How Agent Identity works")
    labels = {f.key: f.title for f in FLOWS}
    key = st.selectbox("Flow", list(labels), format_func=labels.get)
    flow = flow_by_key(key)
    st.write(flow.summary)
    # st.image does not render raw SVG markup, so embed it as HTML.
    st.markdown(f'<div style="overflow-x:auto">{flow_to_svg(flow)}</div>', unsafe_allow_html=True)
    st.markdown("**Key points**")
    for t in flow.takeaways:
        st.markdown(f"- {t}")
    with st.expander("Mermaid source (paste into Docs)"):
        st.code(flow_to_mermaid(flow), language="text")


def page_registry() -> None:
    st.header("Agent registry and lifecycle")
    st.caption("Developer Alice holds writeAgents. Approver Bob holds manageAgentStatus. Nobody approves their own work.")
    st.dataframe(registry_rows(SC.backend.list_agents()), use_container_width=True, hide_index=True)

    st.subheader("Kill switch")
    recs = {r.name: r for r in SC.backend.list_agents()}
    name = st.selectbox("Agent", list(recs))
    rec = recs[name]
    c1, c2, c3 = st.columns(3)
    if c1.button("Suspend (as Bob)", disabled=rec.status.value != AgentStatus.ACTIVE.value):
        SC.service.suspend_agent(APPROVER, rec.agent_id, "suspended from UI")
        st.rerun()
    if c2.button("Reactivate (as Bob)", disabled=rec.status.value == AgentStatus.ACTIVE.value):
        SC.service.reactivate_agent(APPROVER, rec.agent_id)
        st.rerun()
    if c3.button("Try to approve as the creator (Alice)"):
        try:
            SC.service.approve_agent(DEVELOPER, rec.agent_id)
        except IdentityError as exc:
            st.error(f"{type(exc).__name__}: {exc}")
    st.caption("Suspending blocks new tokens. Because issued tokens stay valid, tools also check the live agent "
               "status (strict status check).")

    st.subheader("Create a new agent")
    with st.form("create"):
        new_name = st.text_input("Name", value="demo-agent")
        scopes = st.multiselect("Scope ceiling", ["courses:read", "courses:enroll", "crm:read", "crm:write",
                                                  "ops:read", "ops:restart"], default=["courses:read"])
        if st.form_submit_button("Create (as Alice, status DEFAULT)"):
            try:
                created = SC.service.create_agent(DEVELOPER, new_name.strip(), "Created from the UI", set(scopes))
                st.success(f"Created {created.name} ({created.agent_id}) in status {created.status.value}.")
            except Exception as exc:
                st.error(f"{type(exc).__name__}: {exc}")
    pending = [r for r in SC.backend.list_agents() if r.status.value == AgentStatus.DEFAULT.value]
    if pending:
        target = st.selectbox("Pending approval", [r.name for r in pending])
        if st.button("Approve (as Bob) and register OAuth client"):
            r = SC.backend.find_agent(target)
            SC.service.approve_agent(APPROVER, r.agent_id)
            SC.service.register_client(DEVELOPER, r.agent_id)
            st.rerun()


def _chat_state() -> dict:
    return st.session_state.setdefault("chat", {})


def page_chat() -> None:
    st.header("Chat with agents")
    if SC.engine.engine == FAKE_ENGINE:
        st.warning("No LLM engine is active: answers come from a fake model. Intent guards, approval policies "
                   "and identity checks still run, but tool selection needs a real model. "
                   "Pick one on the **LLM engine** page.")
    c1, c2 = st.columns(2)
    agent_name = c1.selectbox("Agent", list(ALL_SPECS), format_func=lambda n: ALL_SPECS[n].title)
    user = c2.selectbox("Signed-in user", list(USERS), format_func=lambda u: USERS[u]["name"])  # type: ignore[index]
    spec = ALL_SPECS[agent_name]
    key = f"{agent_name}:{user}"
    state = _chat_state().setdefault(key, {"thread": SC.new_thread(), "messages": [], "pending": False})
    st.caption(f"Mode: {spec.access_mode} | ceiling: {', '.join(sorted(spec.allowed_scopes))} | "
               f"{user} entitled to: {', '.join(USERS[user]['scopes'])}")  # type: ignore[arg-type]

    for m in state["messages"]:
        with st.chat_message(m["role"]):
            st.write(m["text"])
            if m.get("meta"):
                with st.expander("Tool calls and policy decisions"):
                    st.json(m["meta"])

    def send(text: str | None, approve: bool | None = None) -> None:
        if text:
            state["messages"].append({"role": "user", "text": text})
        with st.spinner("Agent is working..."):
            turn = RUNNER.run(SC.chat(agent_name, text, user, state["thread"], approve=approve), timeout=300)
        body = f"Error: {turn.error}" if turn.error else (turn.answer or "(no answer)")
        state["messages"].append({"role": "assistant", "text": body,
                                  "meta": {"tool_calls": turn.tool_calls, "decisions": turn.decisions}})
        state["pending"] = turn.needs_approval

    if state["pending"]:
        st.warning("Human-in-the-loop: this action needs your approval.")
        a, d = st.columns(2)
        if a.button("Approve", type="primary"):
            send(None, approve=True)
            st.rerun()
        if d.button("Deny"):
            send(None, approve=False)
            st.rerun()

    st.write("Sample prompts")
    cols = st.columns(max(len(spec.sample_prompts), 1))
    for col, prompt in zip(cols, spec.sample_prompts):
        if col.button(prompt, key=f"{key}:{prompt}", disabled=state["pending"]):
            send(prompt)
            st.rerun()
    typed = st.chat_input("Ask the agent...", disabled=state["pending"])
    if typed:
        send(typed)
        st.rerun()
    if st.button("New conversation"):
        _chat_state()[key] = {"thread": SC.new_thread(), "messages": [], "pending": False}
        st.rerun()


def page_tokens() -> None:
    st.header("Token inspector")
    st.write("Mint a token the way an agent would and see its claims. The signature is not verified here "
             "(display only); tools always verify.")
    c1, c2, c3 = st.columns(3)
    kind = c1.selectbox("Grant", ["End-user login", "Agent: client credentials", "Agent: on behalf of user"])
    user = c2.selectbox("User", list(USERS))
    agent = c3.selectbox("Agent", list(WORKER_SPECS))
    scope_opts = ["courses:read", "courses:enroll", "crm:read", "crm:write", "ops:read", "ops:restart"]
    scopes = st.multiselect("Scopes requested", scope_opts, default=["courses:read"])
    if st.button("Mint token"):
        try:
            rec = SC.backend.find_agent(agent)
            if kind == "End-user login":
                token = SC.login(user)
            elif kind == "Agent: client credentials":
                token = SC.service.agent_token(rec.agent_id, scopes, "demo-api")
            else:
                token = SC.service.delegated_token(rec.agent_id, SC.login(user), scopes, "demo-api")
            st.session_state["token"] = token
        except Exception as exc:
            st.session_state.pop("token", None)
            st.error(f"{type(exc).__name__}: {exc}")
    token = st.session_state.get("token")
    if token:
        summary = inspect_token(token)
        for line in explain_token(summary):
            st.markdown(f"- {line}")
        st.json(summary["raw_claims"])
        st.text_area("Compact JWT", token, height=120)


def page_audit() -> None:
    st.header("Audit trail")
    names = names_by_id()
    c1, c2 = st.columns(2)
    agent = c1.selectbox("Agent", ["(all)"] + list(names.values()))
    outcome = c2.selectbox("Outcome", ["(all)", "success", "denied", "error"])
    agent_id = next((i for i, n in names.items() if n == agent), None)
    events = SC.audit.events(agent_id=agent_id, outcome=None if outcome == "(all)" else outcome)
    st.caption(f"{len(events)} events, newest first. Management and runtime events share the same Agent ID.")
    st.dataframe(audit_rows(reversed(events), names), use_container_width=True, hide_index=True)
    if st.button("Export JSONL to output/"):
        path = SC.audit.export_jsonl(Path(SC.settings.output_dir) / f"audit_{datetime.now():%Y%m%d_%H%M%S}.jsonl")
        st.success(f"Written {path}")


def page_harness() -> None:
    st.header("Harness Kit validation")
    st.write("The **Cuga Harness Kit** scaffolds agent-building skills for coding assistants. This page runs it into a "
             "temporary folder to prove it works, then runs the showcase's own conformance scenarios.")
    if st.button("Run validation", type="primary"):
        from harness.kit import scaffold_and_validate
        from harness.scenarios import run_suite

        with st.spinner("Running..."):
            try:
                rep = scaffold_and_validate()
                st.session_state["kit"] = (rep.ok, rep.kit_version, len(rep.skills_found), rep.problems)
            except Exception as exc:
                st.session_state["kit"] = (False, "?", 0, [str(exc)])
            fake = SC.settings.model_copy(update={"llm_mode": "fake"})
            st.session_state["suite"] = RUNNER.run(run_suite(lambda: Showcase(settings=fake)), timeout=300)
    kit = st.session_state.get("kit")
    suite = st.session_state.get("suite")
    if kit:
        ok, ver, n, problems = kit
        (st.success if ok else st.error)(f"cuga-harness-kit {ver}: {n} skills scaffolded"
                                         + ("" if ok else f" - problems: {problems}"))
    if suite:
        passed = sum(r.passed for r in suite.results)
        (st.success if suite.passed else st.error)(f"Conformance: {passed}/{len(suite.results)} scenarios passed")
        st.dataframe([{"scenario": r.name, "result": "PASS" if r.passed else "FAIL", "ms": r.duration_ms,
                       "detail": r.detail[:120]} for r in suite.results], use_container_width=True, hide_index=True)
        if st.button("Save report to output/"):
            out = Path(SC.settings.output_dir)
            out.mkdir(parents=True, exist_ok=True)
            p = out / f"conformance_{datetime.now():%Y%m%d_%H%M%S}.md"
            p.write_text(suite.to_markdown(), encoding="utf-8")
            st.success(f"Written {p}")


def _clear_engine_form() -> None:
    """Drop the engine form's widget state so the next run shows the newly active values."""
    for k in [k for k in st.session_state if str(k).startswith("llm_")]:
        del st.session_state[k]


def page_llm_engine() -> None:
    st.header("LLM engine")
    st.write("Choose which model backend the agents use. Changes apply to all agents **without a restart**; "
             "running requests finish first. Conversations, approvals, tokens and the audit trail are kept.")
    cur = SC.engine
    (st.warning if cur.engine == FAKE_ENGINE else st.success)(
        f"Active: **{ENGINES[cur.engine].label}** | model `{cur.model or '-'}` | endpoint `{cur.base_url or '-'}` | "
        f"selected from **{cur.source}**")

    mgr = SC.engine_manager
    with st.expander("Detected engines", expanded=True):
        det = st.session_state.get("detections")
        if det is None or st.button("Re-scan"):
            det = st.session_state["detections"] = mgr.detect_all()
        st.dataframe([{"engine": ENGINES[d.engine].label, "status": d.status, "endpoint": d.endpoint or "-",
                       "models": ", ".join(d.models[:6]) or "-", "notes": "; ".join(d.notes)}
                      for d in det.values()], use_container_width=True, hide_index=True)

    ids = [e.engine_id for e in mgr.selectable()]
    default_idx = ids.index(cur.engine) if cur.engine in ids else 0
    engine_id = st.selectbox("Engine", ids, index=default_idx, format_func=lambda i: ENGINES[i].label)
    engine = ENGINES[engine_id]
    st.caption(engine.description)
    base = det[engine_id] if det and engine_id in det else None
    defaults = cur if cur.engine == engine_id else engine.form_defaults(SETTINGS)
    values: dict[str, str] = {}
    for f in engine.fields:
        key = f"llm_{engine_id}_{f.name}"
        if f.secret:
            has_env = bool(SETTINGS.cloud_api_key.get_secret_value()) if engine_id == "cloud" else False
            values[f.name] = st.text_input(f.label, type="password", key=key,
                                           placeholder="(using the key from .env)" if has_env else "",
                                           help=f.help + ". Leave empty to use the key from the environment.")
        else:
            values[f.name] = st.text_input(f.label + (" *" if f.required else ""), value=getattr(defaults, f.name), key=key, help=f.help)
    if base and base.models and "model" in values:
        st.caption("Models reported by the server: " + ", ".join(base.models[:12]))
    remember = st.checkbox("Remember this choice (saves engine, URL and model to .cuga_state/; the API key is never saved)",
                           value=True)

    c1, c2, c3 = st.columns(3)
    result_box = st.container()
    try:
        cfg = config_from_form(engine_id, values, SETTINGS)
        form_error = None
    except LLMEngineError as exc:
        cfg, form_error = None, exc
    if form_error:
        st.error(f"{form_error.message}")
        if form_error.remediation:
            st.info(f"How to fix: {form_error.remediation}")
    # A test result is only valid for the exact form values it was run with. The signature is a
    # hash, so a typed API key is never kept in session state in plaintext.
    sig = hashlib.sha256(repr(sorted((k, str(v)) for k, v in values.items())).encode()).hexdigest()
    if c1.button("Test connection", disabled=cfg is None):
        with st.spinner("Contacting the engine..."):
            st.session_state["engine_test"] = (engine_id, sig, mgr.test(cfg))
    if c2.button("Apply and activate", type="primary", disabled=cfg is None):
        with st.spinner("Testing, draining running requests and switching..."):
            try:
                new = RUNNER.run(SC.switch_engine(cfg), timeout=180)
                st.session_state.pop("engine_test", None)
                _clear_engine_form()
                if remember:
                    mgr.save_override(new)
                st.session_state["engine_msg"] = ("ok", f"Now using {ENGINES[new.engine].label} / {new.model}.", "")
            except LLMEngineError as exc:
                st.session_state["engine_msg"] = ("error", exc.message, exc.remediation + " The previous engine is still active.")
        st.rerun()
    if c3.button("Reset to .env defaults"):
        mgr.clear_override()
        st.session_state.pop("engine_test", None)
        res = mgr.resolve()
        try:
            RUNNER.run(SC.switch_engine(res.config), timeout=180)
            _clear_engine_form()
            st.session_state["engine_msg"] = ("ok", f"Back to {res.config.describe()}.", " ".join(res.warnings))
        except LLMEngineError as exc:
            st.session_state["engine_msg"] = ("error", exc.message, exc.remediation)
        st.rerun()

    test = st.session_state.get("engine_test")
    with result_box:
        if test and test[0] == engine_id and test[1] == sig:
            r = test[2]
            if r.ok:
                st.success(f"{r.message} ({r.latency_ms} ms)")
            else:
                st.error(r.message)
                if r.remediation:
                    st.info(f"How to fix: {r.remediation}")
        msg = st.session_state.pop("engine_msg", None)
        if msg:
            (st.success if msg[0] == "ok" else st.error)(msg[1])
            if msg[2].strip():
                st.info(msg[2].strip())
    if engine_id == "llamacpp":
        st.caption("Tool calling needs a server started with chat-template support, e.g. `llama-server -m model.gguf --jinja --port 8080`.")


def page_settings() -> None:
    st.header("Settings and status")
    st.json({
        "identity_backend": SC.settings.identity_backend,
        "issuer": SC.backend.issuer,
        "token_ttl_seconds": SC.settings.token_ttl_seconds,
        "strict_status_check": SC.settings.strict_status_check,
        "execution_mode": SC.settings.execution_mode,
        "llm_engine": SC.engine.public_dict(),
        "output_dir": SC.settings.output_dir,
        "ui_port": SC.settings.ui_port,
    })
    st.caption("Identity settings come from environment variables or .env (restart to apply). "
               "The LLM engine can be changed at runtime on the **LLM engine** page.")
    with st.expander("Registered tools per agent"):
        st.json({n: [t.name for t in (spec.tool_builder(SC.gateway, SC.store, n) if spec.tool_builder else [])]
                 for n, spec in ALL_SPECS.items()})
    with st.expander("Store state"):
        st.code(json.dumps({"enrollments": SC.store.enrollments, "services": SC.store.services}, indent=2))


PAGE_FUNCS = {
    "Overview": page_overview,
    "How Agent Identity works": page_flows,
    "Agent registry and lifecycle": page_registry,
    "Chat with agents": page_chat,
    "Token inspector": page_tokens,
    "Audit trail": page_audit,
    "Harness Kit validation": page_harness,
    "LLM engine": page_llm_engine,
    "Settings and status": page_settings,
}
PAGE_FUNCS[page]()
