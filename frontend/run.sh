#!/bin/bash
# Chạy frontend: trang web + chuyển tiếp /api sang backend.
# FRONTEND_HOST (mặc định 0.0.0.0), FRONTEND_PORT (mặc định 8080), BACKEND_URL (mặc định http://127.0.0.1:9090)

set -u
FRONTEND_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$FRONTEND_DIR" || exit 1

# Đọc FRONTEND_* trong .env của frontend nếu có
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

: "${FRONTEND_HOST:=0.0.0.0}" "${FRONTEND_PORT:=8080}" "${BACKEND_URL:=http://127.0.0.1:9090}"
BIN="${VENV_BIN:+$VENV_BIN/}"
command -v "${BIN}uvicorn" >/dev/null 2>&1 || { echo "Error: uvicorn not found; set VENV_BIN (dùng chung venv với backend)" >&2; exit 1; }

export BACKEND_URL
echo "Frontend on http://$FRONTEND_HOST:$FRONTEND_PORT -> backend $BACKEND_URL"
exec "${BIN}uvicorn" server:app --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" --proxy-headers
