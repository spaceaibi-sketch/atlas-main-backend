#!/usr/bin/env bash
# Backwards-compatible alias. Prefer run_ai_news_scoring.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$ROOT/run_ai_news_scoring.sh" "$@"
