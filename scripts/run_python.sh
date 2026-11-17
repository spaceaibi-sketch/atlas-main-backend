#!/usr/bin/env bash
# Manual extraction shares the in-project runtime Python packages with systemd
# and Atlas cron jobs. No venv or changes to the .env file.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=../python_runtime.sh
source "$ROOT/python_runtime.sh"
PACKAGES="${ATLAS_BACKEND_PYTHON_PACKAGES:-$(dirname "$ROOT")/runtime/build/backend/python-packages}"
if [[ "$(dirname "$ROOT")" == /opt/atlas && "$PACKAGES" == /opt/atlas_build/backend/python-packages ]]; then
  PACKAGES=/opt/atlas/runtime/build/backend/python-packages
fi
if [[ -d "$PACKAGES" ]]; then
  export PYTHONPATH="$PACKAGES${PYTHONPATH:+:$PYTHONPATH}"
fi
export PYTHONDONTWRITEBYTECODE=1
exec "$SEC_PYTHON_EXE" "$@"
