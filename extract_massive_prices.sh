#!/usr/bin/env bash
# Download 15-minute Massive bars for every ticker in the existing company list.
# Defaults to the last 7 days. --symbol / --limit optional for testing only.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$ROOT/scripts/run_python.sh" -u -m price_pipeline.massive_cli "$@"
