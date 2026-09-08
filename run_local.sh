#!/usr/bin/env bash
set -e
[ -f .env ] || cp .env.example .env
python -m pip install -r requirements.txt
python -m app.worker &
WORKER_PID=$!
trap 'kill $WORKER_PID 2>/dev/null || true' EXIT
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
