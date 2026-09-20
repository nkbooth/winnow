#!/usr/bin/env bash
# Run a command in a one-shot container with the project's toolchain.
#
# The host is an atomic Fedora image and deliberately carries no Python
# toolchain, so every uv/pytest/ruff invocation runs in here. The image matches
# the one CI uses, so a green run locally means the same thing in CI.
set -euo pipefail

IMAGE="${WINNOW_DEV_IMAGE:-ghcr.io/astral-sh/uv:python3.14-bookworm}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# -t only when there is a terminal; CI and scripted runs have none.
TTY_FLAGS=(-i)
[ -t 0 ] && TTY_FLAGS=(-i -t)

exec podman run --rm "${TTY_FLAGS[@]}" \
  -v "$REPO:/w:Z" \
  -w /w \
  -e UV_CACHE_DIR=/w/.uv-cache \
  -e UV_LINK_MODE=copy \
  "$IMAGE" \
  "$@"
