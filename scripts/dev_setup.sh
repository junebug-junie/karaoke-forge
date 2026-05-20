#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip wheel setuptools

if [ ! -f vendor/karaoke-gen/pyproject.toml ]; then
  echo "vendor/karaoke-gen missing; run: git submodule update --init --recursive" >&2
  exit 1
fi

# Forge app + tests
python -m pip install -e ".[generator,dev]"

# Atlas GPU stack (torch cu124, etc.) — install before karaoke-gen so vendor install
# does not upgrade torch via poetry dependencies.
if [ -f requirements-atlas-cu124.txt ]; then
  python -m pip install -r requirements-atlas-cu124.txt
fi

# karaoke-gen code from vendor (editable, --no-deps). Do not pip install karaoke-gen
# from PyPI — it shadows vendor/ and ignores Forge patches. Runtime deps should
# already be present from a prior generator install or requirements-atlas-cu124.txt.
python -m pip uninstall -y karaoke-gen 2>/dev/null || true
python -m pip install -e "./vendor/karaoke-gen[local-whisper]" --no-deps

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
