"""Tests for the UI helper functions (flows, token inspector, tables, Ollama probe)."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from unittest import mock

import pytest

from agent_identity.audit import AuditLog
from agent_identity.ui_helpers import (
    FLOWS,
    audit_rows,
    explain_token,
    flow_by_key,
    flow_to_mermaid,
    flow_to_svg,
    inspect_token,
    probe_ollama,
    registry_rows,
)


def test_six_flows_with_unique_keys():
    keys = [f.key for f in FLOWS]
    assert len(keys) == 6 and len(set(keys)) == 6


def test_flow_by_key_and_unknown():
    assert flow_by_key("obo").key == "obo"
    with pytest.raises(KeyError):
        flow_by_key("nope")


@pytest.mark.parametrize("flow", FLOWS, ids=lambda f: f.key)
def test_flow_svg_is_valid_xml_with_one_label_per_step(flow):
    root = ET.fromstring(flow_to_svg(flow))
    assert root.tag.endswith("svg")
    texts = ["".join(t.itertext()) for t in root.iter() if t.tag.endswith("text")]
    for i, step in enumerate(flow.steps, start=1):
        assert f"{i}. {step.label}" in texts


@pytest.mark.parametrize("flow", FLOWS, ids=lambda f: f.key)
def test_flow_mermaid_has_one_line_per_step(flow):
    lines = flow_to_mermaid(flow).splitlines()
    assert lines[0] == "sequenceDiagram"
    arrows = [ln for ln in lines if "->>" in ln or "-->>" in ln]
    assert len(arrows) == len(flow.steps)


def test_inspect_and_explain_end_user_token(service):
    s = inspect_token(service.user_login("carol", ["a:read"]))
    assert s["subject"] == "carol" and s["actor_chain"] == [] and s["agent_id"] is None
    assert not s["privileged"] and s["expires_in_s"] > 0
    assert "End-user token" in explain_token(s)[0]


def test_inspect_and_explain_autonomous_token(service, active_agent):
    s = inspect_token(service.agent_token(active_agent.agent_id, ["a:read"], "res"))
    assert s["agent_id"] == active_agent.agent_id
    assert "Autonomous" in explain_token(s)[0]


def test_inspect_and_explain_delegated_and_privileged(service, active_agent):
    user = service.user_login("carol", ["a:read", "a:write"])
    tok = service.delegated_token(active_agent.agent_id, user, ["a:write"], "res", [{"type": "x"}])
    s = inspect_token(tok)
    assert s["subject"] == "carol" and s["actor_chain"] == [active_agent.agent_id]
    assert s["privileged"]
    text = "\n".join(explain_token(s))
    assert "Delegated" in text and "Privileged" in text


def test_explain_expired_token(service):
    tok = service.user_login("carol", ["a:read"])
    s = inspect_token(tok, now=10**12)
    assert s["expires_in_s"] < 0 and "Expired." in explain_token(s)


def test_registry_rows(service, active_agent):
    rows = registry_rows(service.backend.list_agents())
    row = next(r for r in rows if r["name"] == "unit-agent")
    assert row["status"] == "ACTIVE" and row["scope ceiling"] == "a:read, a:write"
    assert row["approved_by"] and row["client_id"]


def test_audit_rows_maps_agent_names():
    log = AuditLog()
    log.record("agent.created", agent_id="ag-1", actor="dev", detail={"k": "v"})
    rows = audit_rows(log.events(), {"ag-1": "my-agent"})
    assert rows[0]["agent"] == "my-agent" and rows[0]["event"] == "agent.created"
    assert json.loads(rows[0]["detail"]) == {"k": "v"}
    assert audit_rows(log.events())[0]["agent"] == "ag-1"


def test_probe_ollama_unreachable():
    assert probe_ollama("http://127.0.0.1:9/v1", timeout=0.2) == (False, [])


def test_probe_ollama_success_strips_v1():
    payload = json.dumps({"models": [{"name": "granite4.2:latest"}]}).encode()
    resp = mock.MagicMock()
    resp.read.return_value = payload
    resp.__enter__.return_value = resp
    with mock.patch("urllib.request.urlopen", return_value=resp) as op:
        ok, names = probe_ollama("http://localhost:11434/v1")
    assert ok and names == ["granite4.2:latest"]
    assert op.call_args.args[0] == "http://localhost:11434/api/tags"


def test_probe_ollama_bad_json():
    resp = mock.MagicMock()
    resp.read.return_value = b"not json"
    resp.__enter__.return_value = resp
    with mock.patch("urllib.request.urlopen", return_value=resp):
        assert probe_ollama("http://x") == (False, [])

