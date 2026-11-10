#!/bin/bash
# Bật API và worker Celery; cần RabbitMQ, PostgreSQL chạy sẵn.
# Chandra, PhoWhisper, ProtonX: chạy sẵn hoặc bật ở đây (START_CHANDRA, START_PHOWHISPER, START_SPELLFIX). Cấu hình đọc từ .env.

set -u
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR" || exit 1

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
else
    echo "Warning: .env not found in $PROJECT_DIR (using the current environment)"
fi

BIN="${VENV_BIN:+$VENV_BIN/}"
CELERY="${BIN}celery"
UVICORN="${BIN}uvicorn"

: "${API_PORT:=9090}"
: "${PREPARE_WORKERS:=2}" "${OCR_WORKERS:=2}" "${SPELL_WORKERS:=2}" "${DB_WORKERS:=2}"
: "${SPEECH_PREPARE_WORKERS:=1}" "${SPEECH_ASR_WORKERS:=1}" "${SPEECH_SPELL_WORKERS:=1}" "${SPEECH_DB_WORKERS:=1}"
: "${START_CHANDRA:=false}" "${CHANDRA_CUDA_DEVICE:=0}" "${CHANDRA_PORT:=8101}" "${CHANDRA_MODEL:=datalab-to/chandra-ocr-2}"
: "${CHANDRA_MAX_MODEL_LEN:=6000}" "${CHANDRA_GPU_MEMORY_UTILIZATION:=0.95}"
: "${CHANDRA_MAX_NUM_BATCHED_TOKENS:=9216}" "${CHANDRA_MAX_NUM_SEQS:=20}" "${CHANDRA_PREFIX_CACHING:=true}"
: "${CHANDRA_API_KEY:=}" "${CHANDRA_VLLM_EXTRA_ARGS:=}"
: "${START_PHOWHISPER:=false}" "${PHOWHISPER_CUDA_DEVICE:=0}" "${PHOWHISPER_PORT:=8120}"
: "${PHOWHISPER_MODEL:=vinai/PhoWhisper-large}" "${PHOWHISPER_MODEL_NAME:=phowhisper-large}"
: "${PHOWHISPER_GPU_MEMORY_UTILIZATION:=0.8}" "${PHOWHISPER_MAX_NUM_SEQS:=64}"
: "${PHOWHISPER_API_KEY:=}" "${PHOWHISPER_VLLM_EXTRA_ARGS:=}"
: "${START_SPELLFIX:=false}" "${SPELLFIX_CUDA_DEVICE:=0}"
: "${START_FLOWER:=false}"

fail() { echo "Error: $*" >&2; exit 1; }

[ -n "${CELERY_BROKER_URL:-}" ]     || fail "CELERY_BROKER_URL is not set"
[ -n "${CELERY_RESULT_BACKEND:-}" ] || fail "CELERY_RESULT_BACKEND is not set"
if [ -z "${API_KEYS:-}" ] && [ "${AUTH_DISABLED:-false}" != "true" ]; then
    fail "API_KEYS is not set (or set AUTH_DISABLED=true for local use)"
fi
command -v "$CELERY"  >/dev/null 2>&1 || fail "celery not found; set VENV_BIN"
command -v "$UVICORN" >/dev/null 2>&1 || fail "uvicorn not found; set VENV_BIN"

LOG_DIR="$PROJECT_DIR/logs"
RUN_DIR="$PROJECT_DIR/run"
mkdir -p "$LOG_DIR/api" "$LOG_DIR/prepare" "$LOG_DIR/ocr" "$LOG_DIR/spell" "$LOG_DIR/db" "$LOG_DIR/speech" \
         "$PROJECT_DIR/temp_processing" "$RUN_DIR" \
    || fail "cannot create $LOG_DIR, temp_processing or $RUN_DIR; check that $(whoami) owns $PROJECT_DIR"
[ -w "$LOG_DIR" ] && [ -w "$PROJECT_DIR/temp_processing" ] \
    || fail "logs/ or temp_processing/ is not writable by $(whoami); run: chown -R $(whoami) $PROJECT_DIR"

if [ -s "$RUN_DIR/api.pid" ] && kill -0 "$(cat "$RUN_DIR/api.pid")" 2>/dev/null; then
    fail "services look like they are already running (see $RUN_DIR); run stop-service.sh first"
fi
: > "$RUN_DIR/workers.pid"

STAMP="$(date +%Y%m%d_%H%M%S)"

echo "Starting API on port $API_PORT ..."
nohup "$UVICORN" app.api.main:create_app --factory --host 0.0.0.0 --port "$API_PORT" \
    > "$LOG_DIR/api/api_$STAMP.log" 2>&1 &
echo $! > "$RUN_DIR/api.pid"

start_workers() {  # hàng đợi, thư mục log, số worker, concurrency
    local queue="$1" subdir="$2" count="$3" concurrency="$4" i
    for ((i = 1; i <= count; i++)); do
        nohup "$CELERY" -A app.worker.celery_app worker --loglevel=INFO \
            --queues="$queue" --hostname="${queue}_worker_${i}@%h" --concurrency="$concurrency" \
            --logfile="$LOG_DIR/$subdir/${queue}_worker_${i}_$STAMP.log" \
            > /dev/null 2>&1 &
        echo $! >> "$RUN_DIR/workers.pid"
    done
    echo "  $queue: $count worker(s)"
}

echo "Starting Celery workers ..."
start_workers fullpage_prepare_queue prepare "$PREPARE_WORKERS" 1
start_workers fullpage_ocr_queue     ocr     "$OCR_WORKERS"     1
start_workers fullpage_spell_queue   spell   "$SPELL_WORKERS"   1
start_workers fullpage_db_queue      db      "$DB_WORKERS"      2
start_workers speech_prepare_queue   speech  "$SPEECH_PREPARE_WORKERS" 1
start_workers speech_asr_queue       speech  "$SPEECH_ASR_WORKERS"     1
start_workers speech_spell_queue     speech  "$SPEECH_SPELL_WORKERS"   1
start_workers speech_db_queue        speech  "$SPEECH_DB_WORKERS"      2

if [ "$START_CHANDRA" = "true" ]; then
    # Cổng phải khớp CHANDRA_URL
    command -v "${BIN}vllm" >/dev/null 2>&1 || fail "START_CHANDRA=true but vllm not found; set VENV_BIN"
    chandra_args=(--host 127.0.0.1 --port "$CHANDRA_PORT" --trust-remote-code --disable-log-stats
        --max-model-len "$CHANDRA_MAX_MODEL_LEN" --gpu-memory-utilization "$CHANDRA_GPU_MEMORY_UTILIZATION"
        --max-num-batched-tokens "$CHANDRA_MAX_NUM_BATCHED_TOKENS" --max-num-seqs "$CHANDRA_MAX_NUM_SEQS")
    [ "$CHANDRA_PREFIX_CACHING" = "true" ] && chandra_args+=(--enable-prefix-caching)
    [ -n "$CHANDRA_API_KEY" ] && chandra_args+=(--api-key "$CHANDRA_API_KEY")
    # shellcheck disable=SC2086
    CUDA_VISIBLE_DEVICES="$CHANDRA_CUDA_DEVICE" nohup "${BIN}vllm" serve "$CHANDRA_MODEL" \
        "${chandra_args[@]}" $CHANDRA_VLLM_EXTRA_ARGS \
        > "$LOG_DIR/ocr/chandra_$STAMP.log" 2>&1 &
    echo $! >> "$RUN_DIR/workers.pid"
    echo "Chandra (vLLM) on 127.0.0.1:$CHANDRA_PORT, GPU $CHANDRA_CUDA_DEVICE"
fi

if [ "$START_PHOWHISPER" = "true" ]; then
    # Cổng phải khớp PHOWHISPER_URL; cần vllm[audio]
    command -v "${BIN}vllm" >/dev/null 2>&1 || fail "START_PHOWHISPER=true but vllm not found; set VENV_BIN"
    phowhisper_args=(--host 127.0.0.1 --port "$PHOWHISPER_PORT" --served-model-name "$PHOWHISPER_MODEL_NAME"
        --dtype float16 --max-model-len 448 --disable-log-stats
        --gpu-memory-utilization "$PHOWHISPER_GPU_MEMORY_UTILIZATION" --max-num-seqs "$PHOWHISPER_MAX_NUM_SEQS")
    [ -n "$PHOWHISPER_API_KEY" ] && phowhisper_args+=(--api-key "$PHOWHISPER_API_KEY")
    # shellcheck disable=SC2086
    CUDA_VISIBLE_DEVICES="$PHOWHISPER_CUDA_DEVICE" nohup "${BIN}vllm" serve "$PHOWHISPER_MODEL" \
        "${phowhisper_args[@]}" $PHOWHISPER_VLLM_EXTRA_ARGS \
        > "$LOG_DIR/speech/phowhisper_$STAMP.log" 2>&1 &
    echo $! >> "$RUN_DIR/workers.pid"
    echo "PhoWhisper (vLLM) on 127.0.0.1:$PHOWHISPER_PORT, GPU $PHOWHISPER_CUDA_DEVICE"
fi

if [ "$START_SPELLFIX" = "true" ]; then
    # Không có xác thực: chỉ nghe localhost
    SPELLFIX_CUDA_DEVICE="$SPELLFIX_CUDA_DEVICE" nohup "${BIN}uvicorn" app.services.spellfix_api:app --host 127.0.0.1 --port 8006 \
        > "$LOG_DIR/spell/spellfix_$STAMP.log" 2>&1 &
    echo $! >> "$RUN_DIR/workers.pid"
    echo "ProtonX spellfix on 127.0.0.1:8006, GPU $SPELLFIX_CUDA_DEVICE"
fi

if [ "$START_FLOWER" = "true" ]; then
    [ -n "${FLOWER_BASIC_AUTH:-}" ] || fail "START_FLOWER=true needs FLOWER_BASIC_AUTH (user:password)"
    nohup "$CELERY" -A app.worker.celery_app flower --address=127.0.0.1 --port=5555 \
        --basic-auth="$FLOWER_BASIC_AUTH" > "$LOG_DIR/api/flower_$STAMP.log" 2>&1 &
    echo $! >> "$RUN_DIR/workers.pid"
    echo "Flower on 127.0.0.1:5555"
fi

echo "Done. Logs in $LOG_DIR. Stop with ./stop-service.sh"
