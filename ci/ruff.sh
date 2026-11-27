#!/usr/bin/env bash
# The one way to run Ruff: the pre-push hook and `npm run lint` both call this, so they cannot drift.
# Runs the version pinned in lab/requirements-dev.txt: a repo venv that has exactly that version, else uvx.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
pin=$(sed -n 's/^ruff==\([0-9][0-9.]*\).*/\1/p' lab/requirements-dev.txt)
[ -n "$pin" ] || { echo "ci/ruff.sh: no ruff==<version> pin in lab/requirements-dev.txt" >&2; exit 2; }
for bin in .venv/bin/ruff .venv-checks/bin/ruff; do
  if [ -x "$bin" ] && [ "$("$bin" --version 2>/dev/null)" = "ruff $pin" ]; then exec "$bin" "$@"; fi
done
if command -v uvx >/dev/null 2>&1; then exec uvx --quiet "ruff@$pin" "$@"; fi
echo "ci/ruff.sh: ruff $pin needs uv (uvx) or a repo .venv with ruff $pin; neither found. Install uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
exit 2
