#!/bin/bash
# Dừng frontend start-frontend.sh đã bật.

FRONTEND_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$FRONTEND_DIR/run/frontend.pid"

if [ ! -s "$PID_FILE" ]; then
    echo "Frontend is not running (no $PID_FILE)."
    exit 0
fi

pid="$(cat "$PID_FILE")"
if ! kill -0 "$pid" 2>/dev/null; then
    echo "PID $pid is not running; removing stale pid file."
    rm -f "$PID_FILE"
    exit 0
fi

kill -TERM "$pid" 2>/dev/null
for _ in $(seq 1 15); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
done
if kill -0 "$pid" 2>/dev/null; then
    echo "PID $pid still running after 15s; sending KILL"
    kill -KILL "$pid" 2>/dev/null
fi

rm -f "$PID_FILE"
echo "Frontend stopped."
