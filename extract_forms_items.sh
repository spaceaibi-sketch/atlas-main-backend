#!/usr/bin/env bash
# Canonical News/SEC summary must follow every ingestion run, including partial
# runs that persisted data before a later AI/network error.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for arg in "$@"; do
  case "$arg" in
    -h|--help|--dry-run) exec bash "$ROOT/scripts/run_python.sh" -m sec_edgar.forms_cli "$@" ;;
  esac
done
result=0
bash "$ROOT/scripts/run_python.sh" -m sec_edgar.forms_cli "$@" || result=$?
# A same-pipeline lock collision is temporary, not a successful extraction.
# Do not run the full summary refresh or advance the cron success stamp.
if (( result == 75 )); then
  echo 'Pipeline already running; retry next scheduled hour' >&2
  exit 75
fi
if ! bash "$ROOT/scripts/run_python.sh" -m dashboard.mobile_summary --events-only; then
  echo 'ERROR: Atlas mobile News/SEC summary refresh failed' >&2
  if (( result == 0 )); then result=1; fi
fi
exit "$result"
