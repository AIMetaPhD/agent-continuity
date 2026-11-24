#!/bin/bash
set -e
cd "$(dirname "$0")/.."
if [ ! -x ".venv/bin/python" ]; then
  echo "[Agent Continuity] Creating local Python environment..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install -r requirements.txt
fi
echo "[Agent Continuity] Starting..."
exec .venv/bin/python app.py --open
