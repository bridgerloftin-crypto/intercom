#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="${ROOT}/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "Intercom installer"
echo "Repo: ${ROOT}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python not found: ${PYTHON_BIN}" >&2
  exit 1
fi

if [ ! -d "$VENV" ]; then
  echo "Creating virtualenv..."
  "$PYTHON_BIN" -m venv "$VENV"
fi

echo "Installing dependencies..."
"$VENV/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV/bin/python" -m pip install -r "$ROOT/requirements.txt"

mkdir -p "$ROOT/data"

cat <<EOF

Install complete.

Next steps:

1. Start the server
   $VENV/bin/python $ROOT/server.py

2. Open the UI
   http://localhost:7777

3. Try the CLI
   $VENV/bin/python $ROOT/client.py status
   INTERCOM_AGENT=bridger $VENV/bin/python $ROOT/client.py send forge "hello"

Optional environment variables:
- INTERCOM_PORT
- INTERCOM_DB_PATH
- INTERCOM_BASE
- INTERCOM_NOTIFY_COMMAND

EOF
