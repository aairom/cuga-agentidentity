#!/usr/bin/env bash
# Run unit tests and the Harness Kit conformance suite.
source "$(dirname "$0")/_common.sh"
export LOGURU_LEVEL=ERROR
.venv/bin/python -m pytest -q
.venv/bin/python -m harness
