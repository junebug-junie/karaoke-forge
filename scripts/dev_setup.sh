#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip wheel setuptools
python -m pip install -e ".[generator,dev]"

if [ ! -f .env.local ]; then
  cp .env.example .env.local
  echo "Created .env.local from .env.example"
else
  echo ".env.local already exists; leaving it unchanged"
fi

echo "Setup complete. Run:"
echo "  source .venv/bin/activate"
echo "  set -a && source .env.local && set +a"
echo "  ./scripts/run_dev.sh"
