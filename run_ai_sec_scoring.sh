#!/usr/bin/env bash
# SEC filing scoring: batch size and limits default from .env.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$ROOT/scripts/run_python.sh" -m ai.scoring_cli "$@"
