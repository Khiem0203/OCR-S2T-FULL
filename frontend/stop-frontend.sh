#!/bin/bash
# Dừng frontend

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$PROJECT_DIR/run/frontend.pid"

echo "========================================"
echo "  Stopping Frontend"
echo "========================================"

echo "--> Sending TERM signal to Frontend (Uvicorn) server..."
if [ -s "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    pid="$(cat "$PID_FILE")"
    kill -TERM "$pid"
    echo "    Signal sent."
    for _ in $(seq 1 15); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "    PID $pid still running after 15s; sending KILL"
        kill -KILL "$pid" 2>/dev/null
    fi
else
    echo "    Frontend server not found."
fi
rm -f "$PID_FILE"

echo ""
echo "----------------------------------------"
echo "Frontend has been sent the stop signal."
echo "----------------------------------------"
