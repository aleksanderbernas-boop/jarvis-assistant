#!/usr/bin/env bash
# J.A.R.V.I.S. launcher for macOS/Linux: sets up a virtual environment on first run, then starts the HUD.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  echo "Installing dependencies..."
  .venv/bin/python -m pip install -e ".[desktop]"
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env — add your GROQ_API_KEY, then run this script again."
  exit 0
fi

exec .venv/bin/python -m jarvis serve --open "$@"
