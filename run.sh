#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ -d ".venv/bin" ]; then
  export PATH="$PWD/.venv/bin:$PATH"
fi

HOST="${WX_VIDEO_HOST:-0.0.0.0}"
PORT="${WX_VIDEO_PORT:-18769}"

export PYTHONUNBUFFERED=1
exec python3 app.py --host "$HOST" --port "$PORT"
