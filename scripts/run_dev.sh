#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

source .venv/bin/activate

# Patched karaoke-gen lives in vendor/; karaoke-gen subprocesses get this via runner too.
export PYTHONPATH="${PWD}/vendor/karaoke-gen:${PWD}${PYTHONPATH:+:${PYTHONPATH}}"

APP_MODULE="${APP_MODULE:-packages.karaoke_forge.guarded_app:app}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8790}"

existing_pids=""
if command -v lsof >/dev/null 2>&1; then
  existing_pids="$(lsof -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null || true)"
elif command -v fuser >/dev/null 2>&1; then
  existing_pids="$(fuser "${PORT}"/tcp 2>/dev/null || true)"
fi

if [[ -n "${existing_pids}" ]]; then
  echo "[run-dev] stopping existing listener(s) on port ${PORT}: ${existing_pids}"
  for pid in ${existing_pids}; do
    kill "${pid}" 2>/dev/null || true
  done

  for _ in {1..20}; do
    still_running=""
    if command -v lsof >/dev/null 2>&1; then
      still_running="$(lsof -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null || true)"
    elif command -v fuser >/dev/null 2>&1; then
      still_running="$(fuser "${PORT}"/tcp 2>/dev/null || true)"
    fi
    [[ -z "${still_running}" ]] && break
    sleep 0.25
  done
fi

if command -v lsof >/dev/null 2>&1 && lsof -tiTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[run-dev] port ${PORT} is still occupied after graceful stop; refusing to start duplicate server" >&2
  lsof -iTCP:"${PORT}" -sTCP:LISTEN -n -P >&2 || true
  exit 1
fi

echo "[run-dev] starting ${APP_MODULE} on ${HOST}:${PORT}"
exec uvicorn "${APP_MODULE}" --host "${HOST}" --port "${PORT}"
