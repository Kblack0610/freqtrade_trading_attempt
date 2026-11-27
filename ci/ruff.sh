#!/usr/bin/env bash
# The one way to run Ruff: the pre-push hook and `npm run lint` both call this, so they cannot drift.
# Installs the hash-pinned lab/requirements-dev.lock into .venv-checks with uv (as `npm run checks:env` does), then runs that Ruff.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
if ! command -v uv >/dev/null 2>&1; then
  echo "ci/ruff.sh: uv is not on PATH; it installs the pinned ruff. Install uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 2
fi
uv venv -q --allow-existing --python 3.12 .venv-checks
uv pip sync -q --require-hashes --python .venv-checks lab/requirements-dev.lock
exec .venv-checks/bin/ruff "$@"
