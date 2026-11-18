#!/bin/bash
# Khởi động frontend (trang web tải file lên)

# Cấu hình

# Script nằm ở frontend/, .env cùng thư mục
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    source <(tr -d '\r' < "$ENV_FILE")
    set +a
    echo "Loaded .env from $ENV_FILE"
else
    echo "Warning: .env file not found at $ENV_FILE"
fi

FRONTEND_HOST="${FRONTEND_HOST:-0.0.0.0}"
FRONTEND_PORT="${FRONTEND_PORT:-6767}"
export BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:9090}"

# Môi trường (dùng chung với backend)
# CONDA_ENV: tên env (ocr) hoặc đường dẫn env (/path/to/anaconda3/envs/ocr); để trống = dùng python trong PATH
CONDA_ENV="${CONDA_ENV-${CONDA_ENV_NAME-ocr}}"
CONDA_PATH="${CONDA_PATH:-}"

# Kích hoạt Conda env; không có conda.sh thì thêm thẳng thư mục bin của env vào PATH
activate_conda() {
    if [ -z "$CONDA_ENV" ]; then
        echo "CONDA_ENV is empty, using python in PATH: $(command -v python)"
        return 0
    fi
    local env_dir="" base="" conda_sh="" b
    if [ -d "$CONDA_ENV" ]; then
        env_dir="$(cd "$CONDA_ENV" && pwd)"
        base="$(dirname "$(dirname "$env_dir")")"
    fi
    for b in "$base" "${CONDA_PATH:+$(dirname "$(dirname "$CONDA_PATH")")}" \
             "${CONDA_EXE:+$(dirname "$(dirname "$CONDA_EXE")")}" \
             "$HOME/anaconda3" "$HOME/miniconda3" "/opt/conda" "/opt/anaconda3" "/opt/miniconda3"; do
        if [ -n "$b" ] && [ -f "$b/etc/profile.d/conda.sh" ]; then
            conda_sh="$b/etc/profile.d/conda.sh"
            [ -z "$env_dir" ] && [ -d "$b/envs/$CONDA_ENV" ] && env_dir="$b/envs/$CONDA_ENV"
            break
        fi
    done
    echo "Activating Conda environment: $CONDA_ENV"
    if [ -n "$conda_sh" ]; then
        source "$conda_sh"
        if conda activate "${env_dir:-$CONDA_ENV}"; then
            echo "Conda env active: $CONDA_PREFIX (python: $(command -v python))"
            return 0
        fi
        echo "Warning: conda activate failed, trying env bin directly."
    fi
    if [ -n "$env_dir" ] && [ -x "$env_dir/bin/python" ]; then
        export PATH="$env_dir/bin:$PATH" CONDA_PREFIX="$env_dir"
        echo "Using env bin directly: $env_dir/bin (python: $(command -v python))"
        return 0
    fi
    echo "Error: Conda env '$CONDA_ENV' not found. Set CONDA_ENV in .env to the env path, e.g. /path/to/anaconda3/envs/ocr"
    return 1
}

# Khởi động
LOG_DIR="$PROJECT_DIR/logs"
RUN_DIR="$PROJECT_DIR/run"
mkdir -p "$LOG_DIR" "$RUN_DIR"

activate_conda || exit 1

cd "$PROJECT_DIR" || { echo "Error: Could not change to project directory $PROJECT_DIR"; exit 1; }
command -v uvicorn > /dev/null || { echo "Error: uvicorn not found in Conda env '$CONDA_ENV'"; exit 1; }

# Hàm bật dịch vụ

start_frontend_server() {
    local log_file="$LOG_DIR/frontend_$(date +%Y%m%d_%H%M%S).log"
    if [ -s "$RUN_DIR/frontend.pid" ] && kill -0 "$(cat "$RUN_DIR/frontend.pid")" 2>/dev/null; then
        echo "Frontend already running (PID $(cat "$RUN_DIR/frontend.pid")). Run ./stop-frontend.sh first."
        exit 1
    fi
    if ss -ltn 2>/dev/null | grep -q ":${FRONTEND_PORT} "; then
        echo "Error: port $FRONTEND_PORT is already in use. Change FRONTEND_PORT in .env."
        exit 1
    fi
    echo "Starting Frontend on $FRONTEND_HOST:$FRONTEND_PORT -> backend $BACKEND_URL..."
    nohup uvicorn server:app --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" --proxy-headers > "$log_file" 2>&1 &
    echo $! > "$RUN_DIR/frontend.pid"
    sleep 2
    if ! kill -0 "$(cat "$RUN_DIR/frontend.pid")" 2>/dev/null; then
        rm -f "$RUN_DIR/frontend.pid"
        echo "ERROR: Frontend đã dừng ngay khi khởi động. Cuối log $log_file:"
        tail -n 15 "$log_file"
        exit 1
    fi
    echo "Frontend started. Log: $log_file. PID: $(cat "$RUN_DIR/frontend.pid")"
}

# Chạy
echo "========================================"
echo "  Starting Frontend"
echo "========================================"

start_frontend_server

if ! curl -sf --max-time 3 "$BACKEND_URL/health" > /dev/null; then
    echo "Warning: backend $BACKEND_URL chưa chạy (trang web vẫn mở được nhưng chưa xử lý được file)."
fi

echo ""
echo "--------------------------------"
echo "Trang web: http://<ip-máy>:$FRONTEND_PORT"
echo ""
echo "To stop the frontend, run:"
echo "  ./stop-frontend.sh"
echo "--------------------------------"
