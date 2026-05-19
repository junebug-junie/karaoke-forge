#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

source .venv/bin/activate
if [ -f .env.local ]; then
  set -a
  source .env.local
  set +a
fi

exec uvicorn packages.karaoke_forge.web:app --host 0.0.0.0 --port "${PORT:-8790}"
