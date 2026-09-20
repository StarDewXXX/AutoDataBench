#!/usr/bin/env bash
# Build the base image every task's Dockerfile inherits from
# (FROM automationbench-harbor-base:2). The build context is THIS directory, so
# base-image/Dockerfile can COPY the vendored engine (vendor/) and the abctl
# shell bridge + ab-init/ab-harden (engine/) sitting next to it.
#
# Run once on a fresh machine before running any automationbench task; the tasks
# are otherwise self-contained (each is one FROM + a few KB of world JSON).
# Needs network for pip. Uses the rootless daemon like the rest of AutoDataBench.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${AB_BASE_IMAGE:-automationbench-harbor-base:2}"
export DOCKER_HOST="${DOCKER_HOST:-unix:///run/user/$(id -u)/docker.sock}"
echo "[build] $IMAGE  (context: $HERE)"
docker build -f "$HERE/base-image/Dockerfile" -t "$IMAGE" "$HERE"
echo "[build] done: $IMAGE"
