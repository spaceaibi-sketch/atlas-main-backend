#!/usr/bin/env bash
# Dedicated WebSocket consumer: independent of REST API, cron, and backend.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec /bin/bash "$ROOT/scripts/run_python.sh" -u -m price_pipeline.massive_ws_service "$@"
