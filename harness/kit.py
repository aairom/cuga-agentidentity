"""Cuga Harness Kit integration.

The Harness Kit (https://github.com/cuga-project/cuga-harness-kit) is a CLI that
scaffolds *skills* for AI coding assistants (Claude Code, Cursor, Codex, Bob) so
they can build Cuga agents correctly. It is NOT a test runner, so this module
uses it for what it is good at:

1. :func:`scaffold` runs the real ``cuga-harness-kit init`` into a scratch folder.
2. :func:`validate_scaffold` checks that every expected skill exists and has a
   well-formed ``SKILL.md`` front-matter (``name`` + ``description``).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from agent_identity.logging_setup import get_logger

log = get_logger("harness.kit")

SKILL_PREFIX = "cuga-"
EXPECTED_SKILLS = (
    "author-policy", "build-agent", "build-cuga-skill", "build-supervisor", "build-tool",
    "debug-trajectory", "getting-started", "install-and-launch", "knowledge-rag", "managed-server", "using-cuga",
)


@dataclass
class ScaffoldReport:
    ok: bool
    target_dir: Path
    skills_found: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    kit_version: str = ""


def _kit_executable() -> list[str]:
    """Prefer the console script next to the interpreter, else ``python -m``."""
    exe = Path(sys.executable).parent / "cuga-harness-kit"
    return [str(exe)] if exe.exists() else [sys.executable, "-m", "cuga_harness_kit"]


def scaffold(target: Path | None = None, targets: str = "bob", timeout: int = 120) -> Path:
    """Run ``cuga-harness-kit init`` into ``target`` (a temp dir if omitted). Returns the folder."""
    dest = target or Path(tempfile.mkdtemp(prefix="harness_kit_"))
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [*_kit_executable(), "init", "--targets", targets]
    log.info("running %s in %s", " ".join(cmd), dest)
    try:
        proc = subprocess.run(cmd, cwd=dest, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"cuga-harness-kit could not be executed: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"cuga-harness-kit init failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
    return dest


def _front_matter(text: str) -> dict[str, str]:
    """Parse the minimal ``---`` YAML front-matter of a SKILL.md."""
    if not text.startswith("---"):
        return {}
    block = text.split("---", 2)
    if len(block) < 3:
        return {}
    out: dict[str, str] = {}
    for line in block[1].splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def validate_scaffold(folder: Path, expected: tuple[str, ...] = EXPECTED_SKILLS) -> ScaffoldReport:
    """Check the scaffolded ``.bob/skills`` tree."""
    skills_dir = folder / ".bob" / "skills"
    report = ScaffoldReport(ok=True, target_dir=folder)
    try:
        from importlib.metadata import version

        report.kit_version = version("cuga-harness-kit")
    except Exception:  # pragma: no cover
        report.kit_version = "unknown"
    if not skills_dir.is_dir():
        report.ok = False
        report.problems.append(f"missing directory {skills_dir}")
        return report
    report.skills_found = sorted(p.name for p in skills_dir.iterdir() if p.is_dir())
    for name in expected:
        md = skills_dir / name / "SKILL.md"
        if not md.is_file():
            report.problems.append(f"missing skill: {name}")
            continue
        fm = _front_matter(md.read_text(encoding="utf-8"))
        if not fm.get("name") or not fm.get("description"):
            report.problems.append(f"{name}: SKILL.md front-matter lacks name/description")
        elif fm["name"] not in (name, f"{SKILL_PREFIX}{name}"):
            # The kit prefixes skill names with "cuga-" (e.g. folder build-agent -> name cuga-build-agent).
            report.problems.append(f"{name}: front-matter name '{fm['name']}' does not match folder")
    report.ok = not report.problems
    return report


def scaffold_and_validate(keep: bool = False) -> ScaffoldReport:
    """Convenience: scaffold into a temp dir, validate, clean up unless ``keep``."""
    folder = scaffold()
    try:
        return validate_scaffold(folder)
    finally:
        if not keep:
            shutil.rmtree(folder, ignore_errors=True)
