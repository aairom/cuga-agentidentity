#!/usr/bin/env bash
# Delete the container images built/pulled for this project (Podman).
source "$(dirname "$0")/_common.sh"
command -v podman >/dev/null || { echo "podman not found"; exit 0; }
podman rm -f cuga-agentidentity >/dev/null 2>&1 || true
podman rmi -f cuga-agentidentity:latest >/dev/null 2>&1 || true
podman rmi -f docker.io/library/python:3.12-slim >/dev/null 2>&1 || true
echo "Project images removed."
