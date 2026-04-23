#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"

load_env() {
  [[ -f "$ENV_FILE" ]] || return 0
  set -a
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" ]] && continue
    [[ "$line" != *=* ]] && continue
    eval "export $line"
  done < "$ENV_FILE"
  set +a
}

activate_venv_if_exists() {
  local real_venv
  real_venv="$(readlink -f "$VENV_DIR" 2>/dev/null || echo "$VENV_DIR")"

  if [[ -x "$real_venv/bin/python3" ]]; then
    # 直接用 venv 的 python; 'source activate' 在 NTFS 挂载点上不稳定
    PY_BIN="$real_venv/bin/python3"
    export VIRTUAL_ENV="$real_venv"
    export PATH="$real_venv/bin:$PATH"
  elif [[ -x "$real_venv/bin/python" ]]; then
    PY_BIN="$real_venv/bin/python"
    export VIRTUAL_ENV="$real_venv"
    export PATH="$real_venv/bin:$PATH"
  elif command -v python3 >/dev/null 2>&1; then
    PY_BIN="python3"
  fi
}

# 注意: 变量初始化必须发生在 load_env 之后, 否则 .env 里的
# PULSE_VENV_DIR / PULSE_PYTHON_BIN / PULSE_HOST / PULSE_PORT 不会生效。
load_env
VENV_DIR="${PULSE_VENV_DIR:-$PROJECT_DIR/.venv}"
PY_BIN="${PULSE_PYTHON_BIN:-python}"
HOST="${PULSE_HOST:-0.0.0.0}"
PORT="${PULSE_PORT:-8010}"
activate_venv_if_exists
export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

echo "=== Pulse backend start ==="
echo "PULSE_ENVIRONMENT=${PULSE_ENVIRONMENT:-dev}"
echo "host=$HOST port=$PORT python=$PY_BIN"

if [[ "${PULSE_ENVIRONMENT:-dev}" == "prod" || "${PULSE_RELOAD:-true}" == "false" ]]; then
  echo "mode=production (no reload)"
  exec "$PY_BIN" -m uvicorn pulse.core.server:create_app --factory --host "$HOST" --port "$PORT"
else
  echo "mode=development (reload enabled)"
  exec "$PY_BIN" -m uvicorn pulse.core.server:create_app --factory --host "$HOST" --port "$PORT" --reload
fi
