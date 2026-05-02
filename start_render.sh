#!/usr/bin/env bash
set -euo pipefail

python worker.py &
worker_pid=$!

cleanup() {
  kill "$worker_pid" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

exec gunicorn app:app --bind 0.0.0.0:${PORT:-10000}
