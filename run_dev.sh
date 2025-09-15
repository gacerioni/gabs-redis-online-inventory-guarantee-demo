#!/usr/bin/env bash
set -euo pipefail

if [ ! -d ".venv" ]; then
  echo "venv not found. Run ./setup.sh first." >&2
  exit 1
fi

source .venv/bin/activate

if [ -f ".env" ]; then
  set -a
  source .env
  set +a
else
  echo ".env not found. Copy .env.example to .env and set credentials." >&2
  exit 1
fi

exec uvicorn app_rdi:app --host 0.0.0.0 --port 8000 --reload
