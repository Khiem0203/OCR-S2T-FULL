#!/bin/bash
# Bật frontend chạy nền; cấu hình trong .env.

set -u
FRONTEND_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$FRONTEND_DIR" || exit 1

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

: "${FRONTEND_HOST:=0.0.0.0}" "${FRONTEND_PORT:=6767}" "${BACKEND_URL:=http://127.0.0.1:9090}"
BIN="${VENV_BIN:+$VENV_BIN/}"
if [ -n "${VENV_BIN:-}" ]; then export PATH="$VENV_BIN:$PATH"; fi

fail() { echo "Error: $*" >&2; exit 1; }

command -v "${BIN}uvicorn" >/dev/null 2>&1 || fail "uvicorn not found; set VENV_BIN"

LOG_DIR="$FRONTEND_DIR/logs"
RUN_DIR="$FRONTEND_DIR/run"
PID_FILE="$RUN_DIR/frontend.pid"
mkdir -p "$LOG_DIR" "$RUN_DIR" || fail "cannot create $LOG_DIR or $RUN_DIR"

if [ -s "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    fail "frontend is already running (PID $(cat "$PID_FILE")); run ./stop-frontend.sh first"
fi

export BACKEND_URL
LOG_FILE="$LOG_DIR/frontend_$(date +%Y%m%d_%H%M%S).log"
nohup "${BIN}uvicorn" server:app --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" --proxy-headers \
    > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

sleep 2
if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    rm -f "$PID_FILE"
    tail -n 20 "$LOG_FILE" >&2
    fail "frontend exited right after start (log: $LOG_FILE)"
fi

echo "Frontend on http://$FRONTEND_HOST:$FRONTEND_PORT -> backend $BACKEND_URL"
echo "Log: $LOG_FILE. Stop with ./stop-frontend.sh"
