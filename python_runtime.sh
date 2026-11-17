#!/usr/bin/env bash
# Linux counterpart to python_runtime.bat.
# Can be sourced to export SEC_PYTHON_EXE or executed to check the interpreter.
_SEC_RUNTIME_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Bootstrap interpreter path from .env using literal parsing (no shell eval/source).
if [[ -z "${SEC_PYTHON_EXE:-}" && -f "$_SEC_RUNTIME_ROOT/.env" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^[[:space:]]*SEC_PYTHON_EXE[[:space:]]*=[[:space:]]*(.*)$ ]]; then
      _py_value="${BASH_REMATCH[1]}"
      _py_value="${_py_value%%#*}"
      _py_value="${_py_value#\"}"; _py_value="${_py_value%\"}"
      _py_value="${_py_value#\'}"; _py_value="${_py_value%\'}"
      if [[ -n "$_py_value" ]]; then SEC_PYTHON_EXE="$_py_value"; fi
    fi
  done < "$_SEC_RUNTIME_ROOT/.env"
fi
if [[ -z "${SEC_PYTHON_EXE:-}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    SEC_PYTHON_EXE="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    SEC_PYTHON_EXE="$(command -v python)"
  else
    echo 'ERROR: Python not found. Install Python 3.10+ or set SEC_PYTHON_EXE.' >&2
    return 1 2>/dev/null || exit 1
  fi
fi
if ! "$SEC_PYTHON_EXE" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
  echo "ERROR: SEC_PYTHON_EXE must point to a working Python 3.10+: $SEC_PYTHON_EXE" >&2
  return 1 2>/dev/null || exit 1
fi
export SEC_PYTHON_EXE
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "Python: $SEC_PYTHON_EXE"
  "$SEC_PYTHON_EXE" --version
fi
