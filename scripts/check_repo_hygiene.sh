#!/usr/bin/env bash
# Fails if secrets or local data are tracked or staged. Runs locally and in CI.
# The checks live in check_repo_hygiene.py (they read the git index, not the
# working tree); this wrapper keeps the documented entry point.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
exec python3 scripts/check_repo_hygiene.py
