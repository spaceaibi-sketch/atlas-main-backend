#!/usr/bin/env bash
# Cron invokes this hourly. Wall-clock-hour slot gating honors any positive N hours.
# Each job has its own execution lock. SQLite serializes brief write transactions.
# Independent hourly pipelines may run concurrently without blocking one another.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ $# -ne 2 ]]; then echo "Usage: cron_job.sh JOB_NAME HOURS" >&2; exit 2; fi
NAME="$1"; HOURS="$2"
case "$NAME" in
  extract_recent_filings|extract_forms_items|extract_news|extract_massive_prices|run_ai_sec_scoring|run_ai_news_scoring|run_ai_sec_analys|run_ai_news_analys) ;;
  *) echo 'Unknown scheduled job' >&2; exit 2 ;;
esac
if [[ ! "$HOURS" =~ ^[1-9][0-9]*$ ]] || (( HOURS > 8760 )); then
  echo 'Hours must be between 1 and 8760' >&2; exit 2
fi
mkdir -p "$ROOT/../data/cron_state" "$ROOT/../data/logs"
# Normalize legacy aliases so canonical and old job names cannot run twice.
case "$NAME" in
  run_ai_sec_analys) LOCK_NAME=run_ai_sec_scoring ;;
  run_ai_news_analys) LOCK_NAME=run_ai_news_scoring ;;
  *) LOCK_NAME="$NAME" ;;
esac
# Do not hold a project-wide lock during HTTP/AI processing. Each scheduled
# pipeline is idempotent and prevents a second instance of *itself* instead.
# SQLite coordinates write transactions across independent processes.
command -v flock >/dev/null || { echo 'flock required for scheduled tasks' >&2; exit 1; }
exec 9>"$ROOT/../data/cron_state/$LOCK_NAME.lock"
if ! flock -n 9; then
  echo "$(date -u +%FT%TZ) $NAME: skipped (same job already running)"
  exit 0
fi
STAMP="$ROOT/../data/cron_state/$LOCK_NAME.last_success"
NOW="$(date +%s)"
if [[ -s "$STAMP" ]]; then
  read -r LAST < "$STAMP" || LAST=0
  [[ "$LAST" =~ ^[0-9]+$ ]] || LAST=0
  # Compare whole hour slots, not seconds since the previous job finished.
  # Otherwise an hourly job that finishes after its scheduled minute is
  # incorrectly skipped at the next hourly tick.
  if (( NOW >= LAST && NOW / 3600 - LAST / 3600 < HOURS )); then exit 0; fi
fi
cd "$ROOT"
echo "$(date -u +%FT%TZ) $NAME starting; frequency=${HOURS}h"
if bash "$ROOT/$NAME.sh"; then
  TMP="$STAMP.$$.tmp"
  # Record the successful run's START time so slow jobs do not shift cadence.
  # A failed job never advances the last-success gate.
  printf '%s\n' "$NOW" > "$TMP"
  mv -f "$TMP" "$STAMP"
  echo "$(date -u +%FT%TZ) $NAME completed"
else
  RESULT=$?
  echo "$(date -u +%FT%TZ) $NAME FAILED exit=$RESULT; eligible for retry next hour" >&2
  exit "$RESULT"
fi
