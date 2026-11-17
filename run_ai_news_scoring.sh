#!/usr/bin/env bash
# News Phase 2: batch size and limits default from .env.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$ROOT/scripts/run_python.sh" -m news_pipeline.scoring_cli "$@"
