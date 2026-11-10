#!/bin/bash
# Dừng các tiến trình start-service.sh đã bật (PID trong run/).

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="$PROJECT_DIR/run"

pids=()
for f in "$RUN_DIR/workers.pid" "$RUN_DIR/api.pid"; do
    [ -f "$f" ] && while read -r pid; do [ -n "$pid" ] && pids+=("$pid"); done < "$f"
done

if [ ${#pids[@]} -eq 0 ]; then
    echo "No recorded services to stop."
    exit 0
fi

echo "Sending TERM to ${#pids[@]} process(es); Celery workers finish their current task first ..."
kill -TERM "${pids[@]}" 2>/dev/null

for _ in $(seq 1 60); do
    alive=0
    for pid in "${pids[@]}"; do kill -0 "$pid" 2>/dev/null && alive=1; done
    [ "$alive" -eq 0 ] && break
    sleep 1
done

for pid in "${pids[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
        echo "PID $pid still running after 60s; sending KILL"
        kill -KILL "$pid" 2>/dev/null
    fi
done

rm -f "$RUN_DIR/workers.pid" "$RUN_DIR/api.pid"
echo "Stopped."
