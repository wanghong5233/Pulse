#!/usr/bin/env bash
# start_boss_mcp.sh
# ----------------------------------------------------------------------
# 前台启动 BOSS MCP gateway (pulse.mcp_servers.boss_platform_gateway).
#
# 定位: 与 start_backend.sh 对称.  start.sh all 会在后台拉它.
# 也可以被 boss_mcpctl.sh start 以 nohup 方式拉成 daemon.
#
# 退出语义: 前台持有 uvicorn 子进程 (exec), Ctrl-C / SIGTERM 直接终止.
# ----------------------------------------------------------------------
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

load_env
VENV_DIR="${PULSE_VENV_DIR:-$PROJECT_DIR/.venv}"
PY_BIN="${PULSE_PYTHON_BIN:-python}"
PORT="${PULSE_BOSS_MCP_GATEWAY_PORT:-8811}"
activate_venv_if_exists
export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

echo "=== Pulse BOSS MCP gateway start ==="
echo "PULSE_ENVIRONMENT=${PULSE_ENVIRONMENT:-dev}"
echo "port=$PORT python=$PY_BIN"
# Fail-loud: 露出关键模式配置, 不依赖用户去翻 .env
echo "PULSE_BOSS_MCP_GREET_MODE=${PULSE_BOSS_MCP_GREET_MODE:-browser (default)}"
echo "PULSE_BOSS_MCP_REPLY_MODE=${PULSE_BOSS_MCP_REPLY_MODE:-manual_required (default)}"

# 端口占用检查: 避免两个 gateway 同时跑、静默 EADDRINUSE 后再挂
if command -v ss >/dev/null 2>&1; then
  if ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
    echo "[FATAL] port $PORT already in use."
    echo "        run 'bash scripts/boss_mcpctl.sh status' to inspect."
    exit 2
  fi
fi

exec "$PY_BIN" -m pulse.mcp_servers.boss_platform_gateway
