#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The shared Windows/Linux launcher loads .env safely before importing Dash.
exec bash "$ROOT/scripts/run_python.sh" -m sec_edgar.windows_launcher dash "$@"
