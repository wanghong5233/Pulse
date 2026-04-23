#!/usr/bin/env bash
# boss_mcpctl.sh
# ----------------------------------------------------------------------
# BOSS MCP gateway 进程管理 daemon 工具, 结构对齐 pulsectl.sh.
#
# 为什么独立脚本 (而不是 pulsectl.sh boss_mcp):
#   BOSS MCP 持有 patchright 浏览器 + 用户登录 session, 冷启需 ~30s;
#   backend --reload 高频触发, 不应带飞 MCP 进程, 所以生命周期解耦.
# ----------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
START_SCRIPT="$SCRIPT_DIR/start_boss_mcp.sh"
LOG_FILE="${PULSE_BOSS_MCP_LOG_FILE:-/tmp/pulse_boss_mcp.log}"
PORT="${PULSE_BOSS_MCP_GATEWAY_PORT:-8811}"
HEALTH_URL="http://127.0.0.1:${PORT}/health"

_is_up() {
  curl -sS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1
}

# 进程匹配: 只匹配 python -m 启动的 gateway, 不匹配子 chromium.
_process_pattern() {
  echo "python.*-m pulse.mcp_servers.boss_platform_gateway"
}

start() {
  if _is_up; then
    echo "[boss_mcp] gateway already running"
    status
    return 0
  fi

  echo "[boss_mcp] starting gateway (port=$PORT, log=$LOG_FILE)..."
  nohup bash "$START_SCRIPT" >"$LOG_FILE" 2>&1 </dev/null &

  # gateway 冷启包含 patchright 初始化, 给足窗口
  local i
  for i in $(seq 1 30); do
    sleep 1
    if _is_up; then
      echo "[boss_mcp] started after ${i}s"
      status
      return 0
    fi
  done

  echo "[boss_mcp] start failed (no /health in 30s), recent log:"
  tail -n 60 "$LOG_FILE" 2>/dev/null || true
  return 1
}

stop() {
  local pattern pids
  pattern="$(_process_pattern)"
  # 只匹配 python 进程 (不含 pgrep / grep 自身), 避免误杀当前 shell
  pids="$(pgrep -f "$pattern" || true)"
  if [[ -z "$pids" ]]; then
    echo "[boss_mcp] no gateway process found"
    return 0
  fi

  echo "[boss_mcp] stopping gateway pids: $pids"
  # shellcheck disable=SC2086
  kill $pids || true
  sleep 2
  if pgrep -f "$pattern" >/dev/null 2>&1; then
    echo "[boss_mcp] force killing remaining..."
    pkill -9 -f "$pattern" || true
  fi
  # 清理可能遗留的 patchright/chrome 子进程 (孤儿)
  pkill -9 -f 'patchright/driver/package/cli.js' 2>/dev/null || true
  pkill -9 -f 'chrome-linux64/chrome.*/root/.pulse/boss_browser_profile' 2>/dev/null || true
  echo "[boss_mcp] stopped"
}

restart() {
  stop
  start
}

status() {
  local pattern
  pattern="$(_process_pattern)"
  echo "[boss_mcp] process:"
  pgrep -af "$pattern" || echo "  (none)"
  echo "[boss_mcp] health:"
  if curl -sS --max-time 5 "$HEALTH_URL"; then
    echo ""
  else
    echo "SERVICE_DOWN"
  fi
}

logs() {
  tail -n "${1:-120}" "$LOG_FILE" 2>/dev/null || echo "[boss_mcp] no log file: $LOG_FILE"
}

usage() {
  cat <<EOF
Usage: bash $0 <command>

Commands:
  start        Start BOSS MCP gateway (loads .env via start_boss_mcp.sh)
  stop         Stop gateway + orphan patchright/chrome children
  restart      Restart gateway
  status       Show process + /health JSON (greet_mode, reply_mode, browser_connected)
  logs [N]     Tail log file (default 120 lines)
EOF
}

cmd="${1:-status}"
case "$cmd" in
  start) start ;;
  stop) stop ;;
  restart) restart ;;
  status) status ;;
  logs) logs "${2:-120}" ;;
  *) usage; exit 1 ;;
esac
