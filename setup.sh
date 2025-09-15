#!/usr/bin/env bash
set -euo pipefail

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo
echo "Virtualenv ready."
echo "Next steps:"
echo "  cp .env.example .env   # then edit credentials"
echo "  source .venv/bin/activate"
echo "  set -a; source .env; set +a"
echo "  uvicorn app_rdi:app --reload"
