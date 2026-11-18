#!/bin/bash
# Khởi động OCR toàn trang, trích xuất lời nói, metadata sách

# Cấu hình

# Script nằm ở backend/, .env cùng thư mục
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

# Môi trường
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

# API và worker
API_PORT="${API_PORT:-9090}"
PREPARE_WORKERS="${PREPARE_WORKERS:-2}"; OCR_WORKERS="${OCR_WORKERS:-2}"
SPELL_WORKERS="${SPELL_WORKERS:-2}";     DB_WORKERS="${DB_WORKERS:-2}"
BOOK_WORKERS="${BOOK_WORKERS:-1}"
SPEECH_PREPARE_WORKERS="${SPEECH_PREPARE_WORKERS:-1}"; SPEECH_ASR_WORKERS="${SPEECH_ASR_WORKERS:-1}"
SPEECH_SPELL_WORKERS="${SPEECH_SPELL_WORKERS:-1}";     SPEECH_DB_WORKERS="${SPEECH_DB_WORKERS:-1}"

# Spellfix (ProtonX)
START_SPELLFIX="${START_SPELLFIX:-false}"
SPELLFIX_PORT="${SPELLFIX_PORT:-8006}"
SPELLFIX_CUDA_DEVICE="${SPELLFIX_CUDA_DEVICE:-0}"
SPELLFIX_WAIT="${SPELLFIX_WAIT:-180}"

# Chandra (vLLM)
START_CHANDRA="${START_CHANDRA:-false}"
CHANDRA_PORT="${CHANDRA_PORT:-8101}"
CHANDRA_CUDA_DEVICE="${CHANDRA_CUDA_DEVICE:-0}"
CHANDRA_MODEL="${CHANDRA_MODEL:-datalab-to/chandra-ocr-2}"
CHANDRA_MAX_MODEL_LEN="${CHANDRA_MAX_MODEL_LEN:-8192}"
CHANDRA_GPU_MEMORY_UTILIZATION="${CHANDRA_GPU_MEMORY_UTILIZATION:-0.6}"
CHANDRA_MAX_NUM_BATCHED_TOKENS="${CHANDRA_MAX_NUM_BATCHED_TOKENS:-9216}"
CHANDRA_MAX_NUM_SEQS="${CHANDRA_MAX_NUM_SEQS:-8}"
CHANDRA_PREFIX_CACHING="${CHANDRA_PREFIX_CACHING:-true}"
CHANDRA_WAIT="${CHANDRA_WAIT:-600}"

# PhoWhisper (vLLM)
START_PHOWHISPER="${START_PHOWHISPER:-false}"
PHOWHISPER_PORT="${PHOWHISPER_PORT:-8120}"
PHOWHISPER_CUDA_DEVICE="${PHOWHISPER_CUDA_DEVICE:-0}"
PHOWHISPER_MODEL="${PHOWHISPER_MODEL:-vinai/PhoWhisper-large}"
PHOWHISPER_GPU_MEMORY_UTILIZATION="${PHOWHISPER_GPU_MEMORY_UTILIZATION:-0.5}"
PHOWHISPER_MAX_NUM_SEQS="${PHOWHISPER_MAX_NUM_SEQS:-64}"
PHOWHISPER_WAIT="${PHOWHISPER_WAIT:-300}"

# Flower
START_FLOWER="${START_FLOWER:-false}"

# Khởi động
LOG_DIR="$PROJECT_DIR/logs"
RUN_DIR="$PROJECT_DIR/run"
STAMP="$(date +%Y%m%d_%H%M%S)"
mkdir -p "$LOG_DIR/api" "$LOG_DIR/prepare" "$LOG_DIR/ocr" "$LOG_DIR/spell" "$LOG_DIR/db" \
         "$LOG_DIR/speech" "$LOG_DIR/book" "$LOG_DIR/services"
mkdir -p "$PROJECT_DIR/temp_processing"
mkdir -p "$RUN_DIR"

activate_conda || exit 1

cd "$PROJECT_DIR" || { echo "Error: Could not change to project directory $PROJECT_DIR"; exit 1; }

# Kiểm tra cấu hình
[ -n "$CELERY_BROKER_URL" ]     || { echo "Error: CELERY_BROKER_URL is not set in .env"; exit 1; }
[ -n "$CELERY_RESULT_BACKEND" ] || { echo "Error: CELERY_RESULT_BACKEND is not set in .env"; exit 1; }
if [ -z "$API_KEYS" ] && [ "$AUTH_DISABLED" != "true" ]; then
    echo "Error: API_KEYS is not set (or set AUTH_DISABLED=true for local use)"; exit 1
fi
command -v celery  > /dev/null || { echo "Error: celery not found in Conda env '$CONDA_ENV'"; exit 1; }
command -v uvicorn > /dev/null || { echo "Error: uvicorn not found in Conda env '$CONDA_ENV'"; exit 1; }
if [ -s "$RUN_DIR/api.pid" ] && kill -0 "$(cat "$RUN_DIR/api.pid")" 2>/dev/null; then
    echo "Error: services are already running (API PID $(cat "$RUN_DIR/api.pid")). Run ./stop-service.sh first."
    exit 1
fi
: > "$RUN_DIR/workers.pid"

# Hàm bật dịch vụ

port_in_use() {
    ss -ltn 2>/dev/null | grep -q ":$1 "
}

start_fastapi_server() {
    local log_file="$LOG_DIR/api/api_$STAMP.log"
    if port_in_use "$API_PORT"; then
        echo "Error: port $API_PORT is already in use. Change API_PORT in .env."
        exit 1
    fi
    echo "Starting FastAPI API Server on port $API_PORT..."
    nohup uvicorn app.api.main:create_app --factory --host 0.0.0.0 --port "$API_PORT" > "$log_file" 2>&1 &
    echo $! > "$RUN_DIR/api.pid"
    echo "FastAPI server started. Log: $log_file. PID: $!"
}

# Bật worker Celery: hàng đợi, thư mục log, số worker, concurrency
start_workers() {
    local queue=$1 subdir=$2 count=$3 concurrency=$4 i
    for ((i = 1; i <= count; i++)); do
        nohup celery -A app.worker.celery_app worker --loglevel=INFO \
            --queues="$queue" --hostname="${queue}_worker_${i}@%h" --concurrency="$concurrency" \
            --logfile="$LOG_DIR/$subdir/${queue}_worker_${i}_$STAMP.log" > /dev/null 2>&1 &
        echo $! >> "$RUN_DIR/workers.pid"
    done
    echo "  $queue: $count worker(s), log: $LOG_DIR/$subdir/"
}

# Bật dịch vụ model nếu port còn trống: tên, port, GPU, file log, lệnh...
start_model_service() {
    local name=$1 port=$2 gpu=$3 log_file=$4
    shift 4
    if port_in_use "$port"; then
        echo "$name already running on port $port, skip."
        rm -f "$RUN_DIR/${name}.pid"
        return
    fi
    echo "Starting $name on port $port (GPU $gpu)..."
    CUDA_VISIBLE_DEVICES="$gpu" nohup "$@" > "$log_file" 2>&1 &
    echo $! > "$RUN_DIR/${name}.pid"
    echo "$name started. Log: $log_file. PID: $!"
}

# Chờ dịch vụ nạp model xong
wait_service_ready() {
    local name=$1 port=$2 max_wait=$3 log_file=$4
    local pid=""
    [ -s "$RUN_DIR/${name}.pid" ] && pid="$(cat "$RUN_DIR/${name}.pid")"
    echo "Waiting for $name to load model (max ${max_wait}s)..."
    local waited=0
    while [ "$waited" -lt "$max_wait" ]; do
        if curl -sf --max-time 3 "http://localhost:${port}/health" > /dev/null; then
            echo "$name ready after ${waited}s: $(curl -s --max-time 3 "http://localhost:${port}/health")"
            return 0
        fi
        if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
            echo "ERROR: $name đã dừng khi đang nạp model. Cuối log $log_file:"
            tail -n 15 "$log_file"
            return 0
        fi
        sleep 5
        waited=$((waited + 5))
    done
    echo "Warning: $name chưa sẵn sàng sau ${max_wait}s, vẫn tiếp tục khởi động (xem $log_file)."
    return 0
}

# Chạy
echo "========================================"
echo "  Starting OCR & Speech-to-text Services"
echo "========================================"

# 1. Bật model (chỉ khi START_*=true trong .env)
if [ "$START_SPELLFIX" = "true" ]; then
    log_file="$LOG_DIR/services/spellfix_$STAMP.log"
    export SPELLFIX_CUDA_DEVICE
    start_model_service SPELLFIX "$SPELLFIX_PORT" "$SPELLFIX_CUDA_DEVICE" "$log_file" \
        uvicorn app.services.spellfix_api:app --host 127.0.0.1 --port "$SPELLFIX_PORT"
    wait_service_ready SPELLFIX "$SPELLFIX_PORT" "$SPELLFIX_WAIT" "$log_file"
fi

if [ "$START_CHANDRA" = "true" ]; then
    command -v vllm > /dev/null || { echo "Error: START_CHANDRA=true but vllm not found"; exit 1; }
    log_file="$LOG_DIR/services/chandra_$STAMP.log"
    chandra_args=(--host 127.0.0.1 --port "$CHANDRA_PORT" --trust-remote-code --disable-log-stats
        --max-model-len "$CHANDRA_MAX_MODEL_LEN" --gpu-memory-utilization "$CHANDRA_GPU_MEMORY_UTILIZATION"
        --max-num-batched-tokens "$CHANDRA_MAX_NUM_BATCHED_TOKENS" --max-num-seqs "$CHANDRA_MAX_NUM_SEQS")
    [ "$CHANDRA_PREFIX_CACHING" = "true" ] && chandra_args+=(--enable-prefix-caching)
    [ -n "$CHANDRA_API_KEY" ] && chandra_args+=(--api-key "$CHANDRA_API_KEY")
    start_model_service CHANDRA "$CHANDRA_PORT" "$CHANDRA_CUDA_DEVICE" "$log_file" \
        vllm serve "$CHANDRA_MODEL" "${chandra_args[@]}" $CHANDRA_VLLM_EXTRA_ARGS
    wait_service_ready CHANDRA "$CHANDRA_PORT" "$CHANDRA_WAIT" "$log_file"
fi

if [ "$START_PHOWHISPER" = "true" ]; then
    command -v vllm > /dev/null || { echo "Error: START_PHOWHISPER=true but vllm not found (cần vllm[audio])"; exit 1; }
    log_file="$LOG_DIR/services/phowhisper_$STAMP.log"
    phowhisper_args=(--host 127.0.0.1 --port "$PHOWHISPER_PORT" --dtype float16 --max-model-len 448 --disable-log-stats
        --gpu-memory-utilization "$PHOWHISPER_GPU_MEMORY_UTILIZATION" --max-num-seqs "$PHOWHISPER_MAX_NUM_SEQS")
    [ -n "$PHOWHISPER_API_KEY" ] && phowhisper_args+=(--api-key "$PHOWHISPER_API_KEY")
    start_model_service PHOWHISPER "$PHOWHISPER_PORT" "$PHOWHISPER_CUDA_DEVICE" "$log_file" \
        vllm serve "$PHOWHISPER_MODEL" "${phowhisper_args[@]}" $PHOWHISPER_VLLM_EXTRA_ARGS
    wait_service_ready PHOWHISPER "$PHOWHISPER_PORT" "$PHOWHISPER_WAIT" "$log_file"
fi

# 2. Bật API
start_fastapi_server

# 3. Bật worker Celery
echo "Starting Celery workers..."
start_workers fullpage_prepare_queue prepare "$PREPARE_WORKERS"        1
start_workers fullpage_ocr_queue     ocr     "$OCR_WORKERS"            1
start_workers fullpage_spell_queue   spell   "$SPELL_WORKERS"          1
start_workers fullpage_db_queue      db      "$DB_WORKERS"             2
start_workers speech_prepare_queue   speech  "$SPEECH_PREPARE_WORKERS" 1
start_workers speech_asr_queue       speech  "$SPEECH_ASR_WORKERS"     1
start_workers speech_spell_queue     speech  "$SPEECH_SPELL_WORKERS"   1
start_workers speech_db_queue        speech  "$SPEECH_DB_WORKERS"      2
start_workers book_queue             book    "$BOOK_WORKERS"           2

# 4. Bật Flower
if [ "$START_FLOWER" = "true" ]; then
    [ -n "$FLOWER_BASIC_AUTH" ] || { echo "Error: START_FLOWER=true needs FLOWER_BASIC_AUTH (user:password)"; exit 1; }
    log_file="$LOG_DIR/services/flower_$STAMP.log"
    nohup celery -A app.worker.celery_app flower --address=127.0.0.1 --port=5555 \
        --basic-auth="$FLOWER_BASIC_AUTH" > "$log_file" 2>&1 &
    echo $! > "$RUN_DIR/FLOWER.pid"
    echo "Flower started on 127.0.0.1:5555. Log: $log_file. PID: $!"
fi

echo ""
echo "All application services have been started."
echo "--------------------------------"
echo "Resources Utilization"
echo "RAM Usage:"
command -v free > /dev/null && free -h | awk '/^Mem:/ {print "  Used: "$3" / Total: "$2" (Free: "$4")"}'
if command -v nvidia-smi > /dev/null; then
    echo "VRAM Usage (GPU):"
    nvidia-smi --query-gpu=index,memory.total,memory.used,memory.free --format=csv,noheader,nounits \
        | awk -F', ' '{print "  GPU "$1": Total: "$2" MiB, Used: "$3" MiB, Free: "$4" MiB"}'
fi
echo "--------------------------------"
echo "API: http://localhost:$API_PORT/health"
echo "Logs: $LOG_DIR"
echo ""
echo "To stop the application services, run:"
echo "  ./stop-service.sh"
echo "--------------------------------"
