#!/usr/bin/env bash
# ConnectOnion Studio installer — venv + editable install.
# Dev mode: CONNECTONION_PATH=/path/to/connectonion ./install.sh  (installs your local framework checkout)
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "error: Python 3.11+ required (set PYTHON=/path/to/python3.11)"; exit 1; }

[ -d .venv ] || "$PY" -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install --quiet --upgrade pip

if [ -n "${CONNECTONION_PATH:-}" ]; then
  echo "→ dev mode: connectonion from $CONNECTONION_PATH"
  pip install -e "$CONNECTONION_PATH"
  pip install fastapi "uvicorn[standard]" segno "websockets>=15.0.1,<18" "python-socks[asyncio]>=2.4,<3"
  pip install --no-deps -e .
else
  pip install -e .   # installs the stable ConnectOnion version pinned in pyproject.toml
fi

echo
echo "✓ installed."
if [ ! -f "$HOME/.co/keys.env" ]; then
  echo "  1. .venv/bin/co auth       # one-time: identity + managed model key"
fi
echo "  → .venv/bin/co-studio      # opens http://127.0.0.1:9900"
