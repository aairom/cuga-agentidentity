"""Tests for the Harness Kit wrapper and the conformance suite."""

from __future__ import annotations

import pytest

from agent_identity.config import Settings
from agent_identity.runtime import Showcase
from harness.kit import EXPECTED_SKILLS, _front_matter, scaffold, validate_scaffold
from harness.scenarios import run_suite
from harness.__main__ import main


def test_front_matter_parsing():
    assert _front_matter("---\nname: x\ndescription: y\n---\nbody") == {"name": "x", "description": "y"}
    assert _front_matter("no front matter") == {}
    assert _front_matter("---\nname: x") == {}


def test_validate_scaffold_reports_missing_dir(tmp_path):
    rep = validate_scaffold(tmp_path)
    assert not rep.ok and "missing directory" in rep.problems[0]


def test_validate_scaffold_detects_bad_skill(tmp_path):
    d = tmp_path / ".bob" / "skills" / "build-agent"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: wrong\n---\n")
    rep = validate_scaffold(tmp_path, expected=("build-agent", "ghost"))
    assert not rep.ok
    assert any("lacks name/description" in p for p in rep.problems)
    assert any("missing skill: ghost" in p for p in rep.problems)


def test_real_harness_kit_scaffold_is_valid(tmp_path):
    """Runs the genuine cuga-harness-kit CLI (offline) and validates its output."""
    rep = validate_scaffold(scaffold(tmp_path / "kit"))
    assert rep.ok, rep.problems
    assert set(EXPECTED_SKILLS) <= set(rep.skills_found)


async def test_conformance_suite_all_pass(tmp_path):
    settings = Settings(llm_mode="fake", state_dir=str(tmp_path / "st"), output_dir=str(tmp_path))
    suite = await run_suite(lambda: Showcase(settings=settings))
    assert suite.passed, suite.to_markdown()
    assert len(suite.results) >= 9
    assert "PASS" in suite.to_markdown() and suite.to_dict()["total"] == len(suite.results)


def test_cli_writes_timestamped_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_STATE_DIR", str(tmp_path / "st"))
    assert main(["--output-dir", str(tmp_path)]) == 0
    assert list(tmp_path.glob("conformance_*.md")) and list(tmp_path.glob("conformance_*.json"))
