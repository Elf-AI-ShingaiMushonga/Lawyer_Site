#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

if [[ -d "venv" ]]; then
  # shellcheck source=/dev/null
  source "venv/bin/activate"
elif [[ -d ".venv" ]]; then
  # shellcheck source=/dev/null
  source ".venv/bin/activate"
fi

if [[ $# -eq 0 ]]; then
  set -- --reset --password "ClientDemo2026!"
fi

python app.py seed-demo "$@"
