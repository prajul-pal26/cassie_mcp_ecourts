#!/bin/sh
# Run the FastAPI gateway locally on a vacant port (default 9021).
# Loads secrets from ../.env if present. Uses the shared .venv one level up
# (production/cassie-gateway/.venv) which already has all deps installed;
# override PYBIN to point elsewhere.
cd "$(dirname "$0")"
set -a; . ../.env 2>/dev/null; set +a
export GATEWAY_HOST="${GATEWAY_HOST:-127.0.0.1}"
export GATEWAY_PORT="${GATEWAY_PORT:-9021}"
export ECOURTS_DATA_DIR="${ECOURTS_DATA_DIR:-$PWD/data}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
mkdir -p data

PYBIN="${PYBIN:-./.venv/bin/python}"
exec "$PYBIN" -m uvicorn app.main:app \
    --host "$GATEWAY_HOST" --port "$GATEWAY_PORT" --workers 1
