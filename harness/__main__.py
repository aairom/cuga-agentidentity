"""CLI: ``python -m harness`` - run the Harness Kit validation + conformance suite.

Writes timestamped ``conformance_<ts>.md`` / ``.json`` reports into ``output/``.
Exit code is 0 only when everything passes (usable in CI).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from agent_identity.config import get_settings
from agent_identity.logging_setup import setup_logging
from agent_identity.runtime import Showcase

from .kit import scaffold_and_validate
from .scenarios import run_suite


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m harness", description=__doc__)
    ap.add_argument("--skip-kit", action="store_true", help="skip the cuga-harness-kit scaffold check")
    ap.add_argument("--output-dir", default=None, help="report folder (default: settings.output_dir)")
    args = ap.parse_args(argv)

    settings = get_settings().model_copy(update={"llm_mode": "fake"})  # conformance never needs an LLM
    setup_logging(settings.log_level, settings.output_dir)
    out_dir = Path(args.output_dir or settings.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    ok = True
    if not args.skip_kit:
        try:
            rep = scaffold_and_validate()
            ok &= rep.ok
            print(f"[kit] cuga-harness-kit {rep.kit_version}: {len(rep.skills_found)} skills scaffolded -> "
                  f"{'OK' if rep.ok else 'PROBLEMS: ' + '; '.join(rep.problems)}")
        except RuntimeError as exc:
            ok = False
            print(f"[kit] FAILED: {exc}")

    suite = asyncio.run(run_suite(lambda: Showcase(settings=settings)))
    ok &= suite.passed
    (out_dir / f"conformance_{ts}.md").write_text(suite.to_markdown(), encoding="utf-8")
    (out_dir / f"conformance_{ts}.json").write_text(suite.to_json(), encoding="utf-8")
    for r in suite.results:
        print(f"[{'PASS' if r.passed else 'FAIL'}] {r.name:<22} {r.detail[:90]}")
    print(f"\nReports: {out_dir}/conformance_{ts}.(md|json)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
