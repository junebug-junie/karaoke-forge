#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip wheel setuptools
python -m pip install -e ".[generator,dev]"

cat > .env.local <<'ENV'
KARAOKE_FORGE_ROOT=/mnt/scripts/karaoke-forge
WHISPER_MODEL_SIZE=medium
WHISPER_DEVICE=cuda
ENABLE_LOCAL_WHISPER=true
ENV

echo "Setup complete. Run:"
echo "  source .venv/bin/activate"
echo "  set -a && source .env.local && set +a"
echo "  uvicorn packages.karaoke_forge.web:app --host 0.0.0.0 --port 8790"
