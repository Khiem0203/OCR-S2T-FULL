#!/bin/bash
# Dừng OCR toàn trang, trích xuất lời nói, metadata sách

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="$PROJECT_DIR/run"

echo "========================================"
echo "  Stopping OCR & Speech-to-text Services"
echo "========================================"

# Gửi TERM tới các PID trong file, chờ tối đa max_wait giây rồi KILL
stop_service() {
    local name=$1 pid_file=$2 max_wait=$3
    echo "--> Sending TERM signal to $name..."
    if [ ! -s "$pid_file" ]; then
        echo "    $name not found."
        return
    fi
    local pids=() pid
    while read -r pid; do
        [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && pids+=("$pid")
    done < "$pid_file"
    if [ ${#pids[@]} -eq 0 ]; then
        echo "    $name not running."
        rm -f "$pid_file"
        return
    fi
    kill -TERM "${pids[@]}" 2>/dev/null
    echo "    Signal sent to ${#pids[@]} process(es)."
    local waited=0 alive
    while [ "$waited" -lt "$max_wait" ]; do
        alive=0
        for pid in "${pids[@]}"; do kill -0 "$pid" 2>/dev/null && alive=1; done
        [ "$alive" -eq 0 ] && break
        sleep 1
        waited=$((waited + 1))
    done
    for pid in "${pids[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "    PID $pid still running after ${max_wait}s; sending KILL"
            kill -KILL "$pid" 2>/dev/null
        fi
    done
    rm -f "$pid_file"
}

# Dừng API
stop_service "FastAPI (Uvicorn) server" "$RUN_DIR/api.pid" 15

# Dừng worker (worker làm xong việc đang chạy rồi mới thoát)
stop_service "Celery workers" "$RUN_DIR/workers.pid" 60

# Dừng Flower
stop_service "Flower" "$RUN_DIR/FLOWER.pid" 15

# Dừng model (chỉ những model start-service.sh đã bật)
stop_service "Spellfix service" "$RUN_DIR/SPELLFIX.pid" 30
stop_service "Chandra (vLLM)" "$RUN_DIR/CHANDRA.pid" 60
stop_service "PhoWhisper (vLLM)" "$RUN_DIR/PHOWHISPER.pid" 60

echo ""
echo "----------------------------------------"
echo "All application services have been sent the stop signal."
echo "----------------------------------------"
