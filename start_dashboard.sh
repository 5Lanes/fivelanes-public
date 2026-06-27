#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "Missing virtualenv at $VENV" >&2
  echo "Create it with: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements-linux.txt" >&2
  exit 1
fi

if [[ ! -f "$ROOT/.env" ]]; then
  echo "Warning: $ROOT/.env not found (copy .env.example and set FIVELANES_DATA_ROOT)." >&2
fi

# shellcheck source=/dev/null
source "$VENV/bin/activate"
exec python "$ROOT/dashboard_server.py" "$@"
