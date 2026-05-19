#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

source .venv/bin/activate

exec uvicorn packages.karaoke_forge.web:app --host 0.0.0.0 --port "${PORT:-8790}"
